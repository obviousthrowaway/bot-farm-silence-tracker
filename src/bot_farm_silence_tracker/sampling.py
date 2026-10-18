"""Account sampling.

"Random" here means random among accounts that were active in the seed
communities during discovery. Neither the Reddit nor the X API offers a way to
sample uniformly from all users, so the population is the discovered pool, which
over-represents frequent posters and is biased toward the chosen seeds. Every
statement about prevalence must carry that caveat.

Modes (they combine):

* default: uniform random sample of 3000 accounts from the pool;
* ``n``: uniform random sample of ``n`` accounts;
* ``new_accounts_days``: only accounts created within the last D days. Accounts are
  hydrated first (to learn ``created_at``), then filtered, then sampled. Because
  most of the pool is old, this shrinks the eligible pool a lot; raise
  ``pool_factor`` if too few accounts survive.

Discovery oversamples: ``pool_limit = pool_factor * n``.
"""

from __future__ import annotations

import random
import secrets
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import islice

from bot_farm_silence_tracker.models import STATUS_OPTED_OUT, Account
from bot_farm_silence_tracker.seeds import SeedSet
from bot_farm_silence_tracker.sources.base import AccountUnavailable, Source
from bot_farm_silence_tracker.sources.errors import (
    BudgetExceeded,
    RateLimited,
    SourceAuthError,
    SourceError,
)
from bot_farm_silence_tracker.timeutil import ensure_utc

DEFAULT_SAMPLE_SIZE = 3000
DEFAULT_POOL_FACTOR = 3


def reservoir_sample[T](items: Iterable[T], k: int, rng: random.Random) -> list[T]:
    """Return a uniform random sample of up to ``k`` items from a stream (Algorithm R).

    Every item has the same chance, ``k / N``, of being in the result, in a single
    pass and ``O(k)`` memory. If the stream has at most ``k`` items, all are returned.
    """
    if k < 0:
        raise ValueError("k must be non-negative")
    reservoir: list[T] = []
    for count, item in enumerate(items):
        if count < k:
            reservoir.append(item)
        else:
            slot = rng.randint(0, count)
            if slot < k:
                reservoir[slot] = item
    return reservoir


@dataclass
class SampleResult:
    """The sampled accounts plus bookkeeping about how they were chosen."""

    accounts: list[Account]
    pool_size: int = 0  # Distinct accounts discovered.
    pool_limit: int = 0
    considered: int = 0  # Accounts that passed the new-account filter (or all, if none).
    hydrated: int = 0
    dropped_unknown_age: int = 0
    #: Accounts dropped because they asked not to be shown to logged-out viewers.
    dropped_opted_out: int = 0
    hydrate_errors: int = 0  # Accounts dropped because hydrate raised a plain SourceError.
    truncated: bool = False  # Discovery or hydration stopped early (budget or rate limit).
    #: Sampler warnings plus everything the adapter reported with ``Source.warn``
    #: during discovery.
    warnings: list[str] = field(default_factory=list)


def random_seed() -> int:
    """A fresh seed for a scan without ``--seed``. It is recorded with the run, so a run
    never depends on an unrecorded random state."""
    return secrets.randbelow(2**31)


def sample_accounts(
    source: Source,
    seeds: SeedSet,
    *,
    n: int = DEFAULT_SAMPLE_SIZE,
    new_accounts_days: float | None = None,
    pool_factor: float = DEFAULT_POOL_FACTOR,
    seed: int | None = None,
    now: datetime,
    hydrate: Callable[[Account], Account] | None = None,
    max_consecutive_errors: int | None = None,
) -> SampleResult:
    """Discover a pool from ``source`` and draw a uniform sample of ``n`` accounts.

    Args:
        source: The platform adapter.
        seeds: Seed communities for discovery.
        n: Target sample size.
        new_accounts_days: Keep only accounts created within this many days of ``now``.
        pool_factor: Discovery oversampling; ``pool_limit = ceil(pool_factor * n)``.
        seed: Seeds the sampler's RNG for reproducible draws from the same pool.
        now: Reference time for the account-age filter.
        hydrate: Function used to fill ``created_at``; defaults to ``source.hydrate``.
            The pipeline passes a cache-aware version.
        max_consecutive_errors: Stop discovery (``truncated``, with a warning) after
            this many hydrations in a row fail with a plain ``SourceError``, the
            same cap the fetch loop uses. ``None`` never stops.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    if pool_factor < 1:
        raise ValueError("pool_factor must be at least 1")
    pool_limit = int(-(-pool_factor * n // 1))
    do_hydrate = hydrate or source.hydrate
    rng = random.Random(seed)
    result = SampleResult(accounts=[], pool_limit=pool_limit)
    earliest = ensure_utc(now) - timedelta(days=new_accounts_days) if new_accounts_days else None

    seen: set[str] = set()
    consecutive = [0]

    def stream() -> Iterator[Account]:
        """Yield de-duplicated, filtered accounts, tracking counters on ``result``.

        A budget or rate-limit stop ends the stream cleanly, so the reservoir stays a
        valid uniform sample of the part of the pool that was seen.
        """
        try:
            for account in islice(source.discover(seeds, pool_limit), pool_limit):
                if account.user_id in seen:
                    continue
                seen.add(account.user_id)
                result.pool_size += 1
                if earliest is not None:
                    try:
                        account = do_hydrate(account)
                        result.hydrated += 1
                    except AccountUnavailable as exc:
                        account.status = exc.status
                        if exc.status == STATUS_OPTED_OUT:
                            # Never sampled, and not an unknown age: counted on its own.
                            consecutive[0] = 0
                            result.dropped_opted_out += 1
                            continue
                    except (SourceAuthError, BudgetExceeded, RateLimited):
                        raise
                    except SourceError as exc:
                        result.hydrate_errors += 1
                        consecutive[0] += 1
                        if (
                            max_consecutive_errors is not None
                            and consecutive[0] >= max_consecutive_errors
                        ):
                            result.truncated = True
                            result.warnings.append(
                                f"Discovery aborted after {consecutive[0]} consecutive "
                                f"account lookup errors (last: {exc}); the sample covers "
                                "only the part of the pool seen so far."
                            )
                            return
                        continue
                    consecutive[0] = 0
                    if account.created_at is None:
                        result.dropped_unknown_age += 1
                        continue
                    if ensure_utc(account.created_at) < earliest:
                        continue
                result.considered += 1
                yield account
        except (BudgetExceeded, RateLimited) as exc:
            result.truncated = True
            result.warnings.append(
                f"Discovery stopped early ({exc}); the sample covers only the part of "
                "the pool seen so far. Raise the limit named above or rerun later."
            )

    result.accounts = reservoir_sample(stream(), n, rng)
    if result.hydrate_errors:
        result.warnings.append(
            f"{result.hydrate_errors} discovered accounts were dropped because looking up "
            "their creation date failed."
        )
    # Discovery-time adapter warnings (skipped seeds, could not reach the cutoff).
    result.warnings.extend(source.drain_warnings())
    return result
