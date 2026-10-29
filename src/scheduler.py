"""Random publish times: one base time per video inside the configured Cairo windows, at least
min_gap apart and never on the hour; each platform then gets its own small random offset."""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def _windows(cfg, day: date, tz: ZoneInfo) -> list[tuple[datetime, datetime, float]]:
    """(start, end, weight) — "20:30-22:30 x4" means weight 4; Fridays can have their own list."""
    p = cfg["publishing"]
    spec = p.get("friday_windows") if day.weekday() == 4 and p.get("friday_windows") else p["windows"]
    out = []
    for w in spec:
        rng_part, _, wt = w.partition(" x")
        a, b = rng_part.strip().split("-")
        start = datetime.combine(day, time.fromisoformat(a), tz)
        end = datetime.combine(day, time.fromisoformat(b), tz)
        out.append((start, end, float(wt or 1)))
    return out


def _weighted_pick(rng: random.Random, weights: list[float], k: int) -> list[int]:
    """k distinct indices, more likely the heavier ones (k <= len)."""
    idx, w, out = list(range(len(weights))), list(weights), []
    for _ in range(k):
        i = rng.choices(range(len(idx)), weights=w)[0]
        out.append(idx.pop(i))
        w.pop(i)
    return out


def _rand_minute(rng: random.Random, start: datetime, end: datetime) -> datetime | None:
    span = int((end - start).total_seconds() // 60)
    if span <= 0:
        return None
    for _ in range(20):
        t = start + timedelta(minutes=rng.randint(0, span))
        if t.minute not in (0, 30):          # not on the hour / half hour
            return t.replace(second=rng.randint(0, 59))
    return start + timedelta(minutes=rng.randint(0, span))


def base_times(cfg, rng: random.Random, n: int, day: date, now: datetime | None = None,
               taken: list[datetime] | None = None) -> list[datetime]:
    """n random times on `day` (Cairo), sorted, >= min_gap apart, all after now + lead, and also
    min_gap away from posts already scheduled that day (`taken`) — videos now arrive every 3 h."""
    taken = taken or []
    tz = ZoneInfo(cfg["timezone"])
    p = cfg["publishing"]
    now = (now or datetime.now(tz)).astimezone(tz)
    earliest = now + timedelta(minutes=p["min_lead_minutes"])
    gap = timedelta(minutes=p["min_gap_minutes"])
    wins3 = [(max(s, earliest), e, w) for s, e, w in _windows(cfg, day, tz) if e > earliest]
    wins = [(s, e) for s, e, _ in wins3]
    weights = [w for _, _, w in wins3]
    # many videos in one day: shrink the gap so they still fit inside the peak windows
    window_min = sum((e - s).total_seconds() / 60 for s, e in wins)
    if n + len(taken) > 1:
        gap = min(gap, timedelta(minutes=max(20, 0.6 * window_min / (n + len(taken)))))
    if n == 0:
        return []
    if not wins:
        raise ValueError(f"no publishing window left on {day} after {earliest:%H:%M}")
    for attempt in range(500):
        # one window per video when possible, otherwise reuse windows
        picks = sorted(_weighted_pick(rng, weights, n)) if n <= len(wins) else \
            sorted(rng.choices(range(len(wins)), weights=weights, k=n))
        times = [_rand_minute(rng, *wins[i]) for i in picks]
        if any(t is None for t in times):
            continue
        times.sort()
        if all(b - a >= gap for a, b in zip(times, times[1:])) and \
                all(abs(t - x) >= gap for t in times for x in taken):
            return times
    # day too short for the requested gap (e.g. started late): spread evenly instead
    s, e = wins[0][0], wins[-1][1]
    step = (e - s) / max(1, n)
    return [s + step * k + timedelta(minutes=rng.randint(1, 9)) for k in range(n)]


def platform_times(cfg, rng: random.Random, base: datetime, platforms: list[str]) -> dict[str, datetime]:
    """Same video, each platform at base + its own random offset (>= 5 min apart)."""
    lo, hi = cfg["publishing"]["platform_offset_minutes"]
    order = platforms[:]
    rng.shuffle(order)
    out, used = {}, []
    for i, p in enumerate(order):
        for _ in range(50):
            off = 0 if i == 0 else rng.randint(lo, hi)
            if all(abs(off - u) >= 5 for u in used):
                break
        used.append(off)
        out[p] = base + timedelta(minutes=off, seconds=rng.randint(0, 59))
    return out
