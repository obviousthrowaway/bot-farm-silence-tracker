"""Deterministic synthetic source for tests and ``--source fake`` demo runs.

The world is a fixed population of accounts, each assigned an archetype by a seeded
hash. The same ``world_seed``, cutoff and ``now`` always produce the same accounts
and activity, regardless of the sampling ``--seed``. Archetypes:

* ``cliff_bot``: posts at a steady cadence every day, then stops within 48 hours
  before the cutoff. Most stop in one of a few synchronized batches (simulating a
  shared data center going offline); some stop alone.
* ``steady_active``: posts at a steady cadence and keeps going (a control).
* ``human_irregular``: posts on a random minority of days at daytime hours.
* ``human_break``: posts fairly regularly, then takes a break that starts before the
  cutoff; half of them return after the cutoff. A few break right at the cutoff,
  which makes them realistic false positives.
* ``long_dormant``: was regular long ago and silent through the baseline window.
* ``new_account``: created within the last 60 days, irregular.
* ``suspended`` / ``deleted`` / ``private``: raise :class:`AccountUnavailable`.

``Account.meta["archetype"]`` records the ground truth for tests. Scoring never
reads it.

The world is anchored on the cutoff and ``now`` the source was built with, so a
placebo sample (:meth:`Source.shifted`) sees the same world from earlier weeks.

The churn world
---------------
``options["world"] = "churn"`` is a pure null for the placebo comparison: there is no
outage. Account ``i`` is created at a uniform time, posts as a Poisson process at its
own rate (``post_rate`` per day on average, every hour of the day alike) and stops
for good after an exponential lifetime (``churn_days`` on average), so permanent
stops happen at the same rate in every week. Discovery selects the way the real
adapters do: it yields, in a seeded random order, only accounts with activity in the
discovery window, the baseline ``[baseline_start, cutoff)`` or, with
``config.active_within_days`` D (set by library callers; the CLI refuses
``--active-within-days`` for this platform), ``[max(baseline_start, now - D),
min(now, cutoff))``.
``outage_share`` (default 0) makes that share of the accounts alive at the anchored
cutoff stop for good within three hours before it: a synchronized outage, the
positive control.
"""

from __future__ import annotations

import random
from bisect import bisect_left
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import ClassVar

from bot_farm_silence_tracker.models import Account, Activity, content_hash
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.base import (
    AccountUnavailable,
    HostUse,
    NewestActivity,
    Source,
    SourceConfig,
)

#: (archetype, cumulative probability) pairs used to assign archetypes.
_ARCHETYPE_CDF: tuple[tuple[str, float], ...] = (
    ("cliff_bot", 0.04),
    ("steady_active", 0.08),
    ("human_break", 0.16),
    ("long_dormant", 0.26),
    ("new_account", 0.36),
    ("suspended", 0.39),
    ("deleted", 0.40),
    ("private", 0.41),
    ("human_irregular", 1.0),
)

#: Hours before the cutoff at which the synchronized bot batches go silent.
_BATCH_STOP_HOURS_BEFORE_CUTOFF = (3.0, 11.5, 30.0)

_DISCOVERY_PAGE = 100


class FakeSource(Source):
    """Synthetic platform with a known mix of account behaviors."""

    name: ClassVar[str] = "fake"
    required_credentials: ClassVar[tuple[str, ...]] = ()
    #: Synthetic data: no host is ever contacted, so every request is refused.
    network_hosts: ClassVar[tuple[HostUse, ...]] = ()
    #: Every fetch is a pure function of the account (its own seeded RNGs) and the
    #: shared client's budget is thread safe, so tests can run it with --concurrency.
    max_concurrency: ClassVar[int] = 3
    #: Fetches are pure functions of the window, so two-phase sampling works.
    supports_baseline_sampling: ClassVar[bool] = True

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        self.population = int(config.options.get("population", 6000))
        self.world_seed = int(config.options.get("world_seed", 20261010))
        self.world = str(config.options.get("world", "archetypes"))
        if self.world not in ("archetypes", "churn"):
            raise ValueError(f"option 'world' must be archetypes or churn, got {self.world!r}")
        self.post_rate = float(config.options.get("post_rate", 2.5))
        self.churn_days = float(config.options.get("churn_days", 100.0))
        self.outage_share = float(config.options.get("outage_share", 0.0))
        # The world stays where it was built, whatever placebo sample is drawn from it.
        self._world_cutoff = config.cutoff
        self._world_now = config.now
        self._churn: dict[int, tuple[datetime, list[datetime]]] = {}

    # -- helpers -----------------------------------------------------------------

    def _rng(self, *parts: object) -> random.Random:
        return random.Random(":".join(str(p) for p in (self.world_seed, *parts)))

    @staticmethod
    def _index(user_id: str) -> int:
        return int(user_id.removeprefix("fake_"))

    def archetype(self, index: int) -> str:
        """Return the archetype assigned to account ``index``."""
        if self.world == "churn":
            return "churn"
        roll = self._rng("archetype", index).random()
        for name, edge in _ARCHETYPE_CDF:
            if roll < edge:
                return name
        return "human_irregular"  # pragma: no cover - the CDF ends at 1.0

    def _created_at(self, index: int, archetype: str) -> datetime:
        if self.world == "churn":
            return self._churn_account(index)[0]
        rng = self._rng("created", index)
        cutoff, now = self._world_cutoff, self._world_now
        if archetype == "new_account":
            return now - timedelta(days=rng.uniform(0.5, 60.0))
        if archetype == "cliff_bot":
            return cutoff - timedelta(days=rng.uniform(15.0, 150.0))
        return cutoff - timedelta(days=rng.uniform(120.0, 2500.0))

    def _make_account(self, index: int, community: str | None = None) -> Account:
        archetype = self.archetype(index)
        meta: dict[str, object] = {"archetype": archetype}
        if community:
            meta["community"] = community
        return Account(
            platform=self.name,
            user_id=f"fake_{index:06d}",
            handle=f"fake_user_{index:06d}",
            created_at=None,
            meta=meta,
        )

    # -- Source interface --------------------------------------------------------

    def placebo_discovery_problem(self, seeds: SeedSet) -> str | None:
        """Synthetic data reaches back as far as asked."""
        del seeds
        return None

    def discover(self, seeds: SeedSet, pool_limit: int) -> Iterator[Account]:
        if self.world == "churn":
            yield from self._discover_churn(seeds, pool_limit)
            return
        order = list(range(self.population))
        self._rng("discovery-order").shuffle(order)
        for count, index in enumerate(order[:pool_limit]):
            if count % _DISCOVERY_PAGE == 0:
                self.http.spend()  # One simulated listing request per page of results.
            community = self._rng("home", index).choice(seeds.items)
            yield self._make_account(index, community)

    def hydrate(self, account: Account) -> Account:
        if account.created_at is not None:
            return account
        self.http.spend()
        index = self._index(account.user_id)
        archetype = self.archetype(index)
        if archetype in ("suspended", "deleted", "private"):
            raise AccountUnavailable(archetype)
        account.created_at = self._created_at(index, archetype)
        return account

    def fetch_activity(self, account: Account, since: datetime, until: datetime) -> list[Activity]:
        self.http.spend()
        return self._window(account, since, until)

    def newest_activity(
        self, account: Account, kind: str, since: datetime, until: datetime
    ) -> NewestActivity:
        """One simulated ``sort=desc, limit=1`` request: the newest item of ``kind``."""
        self.http.spend()
        stamps = [a.ts for a in self._window(account, since, until) if a.kind == kind]
        return NewestActivity(max(stamps) if stamps else None, since)

    def _window(self, account: Account, since: datetime, until: datetime) -> list[Activity]:
        """The account's activity in ``[since, until)``; makes no request."""
        index = self._index(account.user_id)
        archetype = self.archetype(index)
        if archetype in ("suspended", "deleted", "private"):
            raise AccountUnavailable(archetype)
        community = str(account.meta.get("community") or "fake_community")
        timestamps = self._timeline(index, archetype)
        rng = self._rng("content", index)
        pool = [f"canned message {k}" for k in range(5)]
        activities: list[Activity] = []
        for i, ts in enumerate(timestamps):
            if not since <= ts < until:
                continue
            is_bot = archetype in ("cliff_bot", "steady_active")
            if is_bot and rng.random() < 0.3:
                digest = content_hash(rng.choice(pool))
            else:
                digest = content_hash(f"{account.user_id}:{i}")
            kind = "post" if rng.random() < 0.3 else "comment"
            activities.append(Activity(self.name, account.user_id, ts, kind, community, digest))
        return activities

    # -- timeline generation -----------------------------------------------------

    def _timeline(self, index: int, archetype: str) -> list[datetime]:
        if archetype == "churn":
            return self._churn_account(index)[1]
        rng = self._rng("timeline", index)
        cutoff, now = self._world_cutoff, self._world_now
        created = self._created_at(index, archetype)
        history_start = max(created, cutoff - timedelta(days=90))
        match archetype:
            case "cliff_bot":
                return self._cadence(rng, history_start, self._bot_stop_time(index, rng))
            case "steady_active":
                return self._cadence(rng, history_start, now)
            case "human_irregular":
                return self._human(rng, history_start, now, day_prob=rng.uniform(0.08, 0.35))
            case "new_account":
                return self._human(rng, created, now, day_prob=rng.uniform(0.2, 0.6))
            case "long_dormant":
                end = cutoff - timedelta(days=rng.uniform(40.0, 100.0))
                start = end - timedelta(days=60)
                return self._human(rng, max(created, start), end, day_prob=0.7)
            case "human_break":
                break_start = cutoff - timedelta(days=rng.uniform(0.5, 25.0))
                times = self._human(rng, history_start, break_start, day_prob=0.7)
                if rng.random() < 0.5:
                    resume = cutoff + timedelta(days=rng.uniform(1.0, 8.0))
                    times += self._human(rng, resume, now, day_prob=0.5)
                return times
        return []  # pragma: no cover - unavailable archetypes never reach here

    def _bot_stop_time(self, index: int, rng: random.Random) -> datetime:
        cutoff = self._world_cutoff
        if self._rng("batched", index).random() < 0.7:
            batch = self._rng("batch", index).randrange(len(_BATCH_STOP_HOURS_BEFORE_CUTOFF))
            hours = _BATCH_STOP_HOURS_BEFORE_CUTOFF[batch]
            return cutoff - timedelta(hours=hours, minutes=rng.uniform(0.0, 15.0))
        return cutoff - timedelta(hours=rng.uniform(0.5, 47.0))

    # -- the churn world ----------------------------------------------------------

    def _churn_account(self, index: int) -> tuple[datetime, list[datetime]]:
        """``(created_at, timestamps)`` of churn account ``index`` (memoized)."""
        found = self._churn.get(index)
        if found is not None:
            return found
        rng = self._rng("churn", index)
        cutoff, now = self._world_cutoff, self._world_now
        # Long enough before the cutoff for eight placebo weeks and their baselines.
        history = cutoff - timedelta(days=110)
        created = cutoff - timedelta(days=rng.uniform(-(now - cutoff).days, 250.0))
        stop = created + timedelta(days=rng.expovariate(1 / self.churn_days))
        if self.outage_share > 0 and created < cutoff < stop and rng.random() < self.outage_share:
            stop = cutoff - timedelta(hours=rng.uniform(0.0, 3.0))
        rate = rng.uniform(0.6, 1.4) * self.post_rate / 86400  # Posts per second.
        times: list[datetime] = []
        t = max(created, history)
        end = min(stop, now)
        while True:
            t += timedelta(seconds=rng.expovariate(rate))
            if t >= end:
                break
            times.append(t.replace(microsecond=0))
        found = (created, times)
        self._churn[index] = found
        return found

    def _churn_window(self) -> tuple[datetime, datetime]:
        """The discovery window of the churn world (see the module notes)."""
        cutoff = self.config.cutoff
        start = self.config.baseline_start or cutoff - timedelta(days=30)
        days = self.config.active_within_days
        if days is None:
            return start, cutoff
        now = self.config.now
        return max(start, now - timedelta(days=days)), min(now, cutoff)

    def _discover_churn(self, seeds: SeedSet, pool_limit: int) -> Iterator[Account]:
        start, end = self._churn_window()
        order = list(range(self.population))
        self._rng("churn-discovery", self.config.seed).shuffle(order)
        found = 0
        for count, index in enumerate(order):
            if found >= pool_limit:
                return
            if count % _DISCOVERY_PAGE == 0:
                self.http.spend()  # One simulated search request per page.
            times = self._churn_account(index)[1]
            position = bisect_left(times, start)
            if position < len(times) and times[position] < end:
                found += 1
                yield self._make_account(index, seeds.items[index % len(seeds.items)])

    @staticmethod
    def _cadence(rng: random.Random, start: datetime, end: datetime) -> list[datetime]:
        """Generate evenly spaced, lightly jittered timestamps walking back from ``end``."""
        per_day = rng.randint(3, 8)
        step = timedelta(hours=24 / per_day)
        times: list[datetime] = []
        cursor = end - timedelta(seconds=1)
        while cursor >= start:
            times.append(cursor.replace(microsecond=0))
            cursor -= step * rng.uniform(0.85, 1.15)
        return sorted(times)

    @staticmethod
    def _human(
        rng: random.Random, start: datetime, end: datetime, day_prob: float
    ) -> list[datetime]:
        """Generate daytime-biased posts on a random subset of days."""
        if end <= start:
            return []
        peak_hour = rng.uniform(9.0, 21.0)
        times: list[datetime] = []
        day = start.replace(hour=0, minute=0, second=0, microsecond=0)
        while day < end:
            if rng.random() < day_prob:
                for _ in range(rng.randint(1, 5)):
                    hour = min(23.99, max(0.0, rng.gauss(peak_hour, 3.0)))
                    ts = (day + timedelta(hours=hour)).replace(microsecond=0)
                    if start <= ts < end:
                        times.append(ts)
            day += timedelta(days=1)
        return sorted(times)
