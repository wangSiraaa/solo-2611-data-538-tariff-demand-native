"""Load facts and the interval-aware demand report.

Load records describe piecewise-constant power over real UTC intervals.  All
arithmetic (integral, energy, cost, demand) is exact ``Fraction`` arithmetic;
nothing goes through binary floating point.

The demand scan works from *cumulative* curves rather than samples.  The window
average is piecewise linear in the start second; its slope and eligibility can
only change where the window starts or ends hits a load endpoint or a tariff
boundary.  Evaluating the complete candidate set {b, b-W} for every breakpoint
b therefore finds the true rolling extreme - including windows that end in the
middle of a load row, which fixed-bucket/resample schemes miss.
"""
from bisect import bisect_right
from collections.abc import Mapping
from copy import deepcopy
from fractions import Fraction


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _rational(value, label="value"):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a rational number")
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            raise ValueError(f"{label} must not be empty")
        try:
            return Fraction(text)
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError(f"{label} must be a rational number") from exc
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    raise ValueError(f"{label} must be a rational number string")


class LoadWarehouse:
    def __init__(self):
        self.series = {}

    # ------------------------------------------------------------------ writes
    def put(self, series, rows):
        """Atomically replace one load series (validate fully, then commit)."""
        if not isinstance(series, str) or series == "":
            raise ValueError("series must be a non-empty string")
        if not isinstance(rows, (list, tuple)):
            raise ValueError("rows must be a sequence")

        saved = []
        seen = set()
        previous_end = None
        for row in sorted(rows, key=lambda r: (r["start"], r["end"])):
            if not isinstance(row, Mapping):
                raise ValueError("each load row must be a mapping")
            try:
                ident = row["id"]
                start = row["start"]
                end = row["end"]
                kw_text = row["kw"]
            except KeyError as exc:
                raise ValueError("load row is missing a required field") from exc

            if not isinstance(ident, str) or ident == "":
                raise ValueError("id must be a non-empty string")
            if ident in seen:
                raise ValueError("duplicate id")
            if not _is_int(start) or not _is_int(end) or start >= end:
                raise ValueError("load row needs integer start < end")
            if previous_end is not None and start < previous_end:
                raise ValueError("load rows must not overlap")
            kw = _rational(kw_text, "kw")
            if kw < 0:
                raise ValueError("kw must be non-negative")

            seen.add(ident)
            saved.append({"id": ident, "start": start, "end": end, "kw": kw})
            previous_end = end

        # Commit only after everything validated, so a rejected replacement
        # leaves the previous series untouched.
        self.series[series] = saved

    # ------------------------------------------------------------------ query
    def query(self, series, start, end, provider, window_seconds=900):
        if not _is_int(start) or not _is_int(end) or start >= end:
            raise ValueError("invalid query range")
        if not _is_int(window_seconds) or window_seconds <= 0:
            raise ValueError("window_seconds must be a positive integer")
        if not hasattr(provider, "segments") or not callable(provider.segments):
            raise ValueError("provider must expose segments()")

        # One complete snapshot: materialise and copy exactly once, then use it
        # for the whole query (never re-read the provider and never fall back to
        # any local catalogue or an earlier snapshot).
        bands = deepcopy(list(provider.segments(start, end)))
        _check_bands(bands, start, end)

        rows = []
        for row in self.series.get(series, []):
            left = max(row["start"], start)
            right = min(row["end"], end)
            if left < right:
                rows.append((left, right, row["kw"], row["id"]))
        rows.sort(key=lambda item: item[0])

        # Clip the snapshot to the request (providers already cover it, but a
        # third party may return the superset it likes).
        clipped = []
        for band in bands:
            left = max(band["start"], start)
            right = min(band["end"], end)
            if left < right:
                clipped.append(
                    (left, right, Fraction(band["rate"]), band["band"], band["peak"])
                )

        energy, cost, coverage, groups = _account(rows, clipped)

        demand, window, sources = self._demand(
            rows, clipped, start, end, window_seconds
        )

        return {
            "energy_kwh": str(energy),
            "cost": str(cost),
            "by_band": [
                {"band": name, "energy_kwh": str(value[0]), "cost": str(value[1])}
                for name, value in sorted(groups.items())
            ],
            "coverage_seconds": coverage,
            "missing_seconds": (end - start) - coverage,
            "demand_kw": demand,
            "window": window,
            "window_sources": sources,
        }

    # ------------------------------------------------------------------ demand
    def _demand(self, rows, clipped, start, end, window):
        first_s = start
        last_s = end - window
        if first_s > last_s:
            return None, None, []

        load_curve = _build_load_curve(rows, start, end)
        peak_curve = _build_peak_curve(clipped, start, end)

        # The window average f(s) = (E(s+W) - E(s)) * 3600 / W is piecewise
        # linear in the start second s.  Its slope can only change where s or
        # s+W crosses a knot of a cumulative curve, i.e. at ``bp`` or
        # ``bp - W`` for every breakpoint bp (load endpoints and tariff
        # boundaries).  Eligibility can only change at the same places.  Hence
        # the maximum over integer s in [first_s, last_s] is attained at one of
        # those candidates or at a domain endpoint; evaluating the complete
        # candidate set is exact and needs no sampled buckets.
        breakpoints = [boundary for left, right, _kw, _id in rows for boundary in (left, right)]
        breakpoints += [boundary for left, right, _r, _n, _p in clipped
                        for boundary in (left, right)]

        # Slope/eligibility can change at ``b`` or ``b - W``.  Evaluate those
        # breakpoints and their integer neighbours, plus the domain ends, so
        # no integer rounding at an open interval edge is ever missed.
        candidates = {first_s, last_s}
        for boundary in breakpoints:
            for edge in (boundary, boundary - window):
                for s in (edge - 1, edge, edge + 1):
                    if first_s <= s <= last_s:
                        candidates.add(s)

        best = None
        best_start = None
        for s in sorted(candidates):
            energy, observed = _curve_at(load_curve, s, window)
            if observed != window:
                continue
            peak = _peak_between(peak_curve, s, s + window)
            if peak != window:
                continue
            # Strict comparison keeps the earliest start on ties.
            if best is None or energy > best:
                best, best_start = energy, s

        if best is None:
            return None, None, []
        window_end = best_start + window
        selected = [
            ident
            for left, right, _kw, ident in rows
            if min(right, window_end) - max(left, best_start) > 0
        ]
        return str(best * 3600 / window), [best_start, window_end], selected


def _check_bands(bands, start, end):
    """Enforce that a provider returned seamless, ordered, covering segments.

    A provider may return the exact ``[start, end)`` request or a valid superset
    of it; either way it must be seamless and ordered.  The caller clips the
    snapshot, so the returned boundaries are what gets consumed.
    """
    previous = None
    for band in bands:
        if not isinstance(band, Mapping):
            raise ValueError("provider segment must be a mapping")
        try:
            left = band["start"]
            right = band["end"]
            rate_text = band["rate"]
            name = band["band"]
            peak = band["peak"]
        except KeyError as exc:
            raise ValueError("provider segment is missing a required field") from exc
        if not _is_int(left) or not _is_int(right) or left >= right:
            raise ValueError("provider segment has an invalid interval")
        if previous is None:
            if left > start:
                raise ValueError("provider segments must cover the whole request")
        elif left != previous:
            raise ValueError("provider segments must be seamless and ordered")
        if not isinstance(name, str) or name == "":
            raise ValueError("provider band must be a non-empty string")
        _rational(rate_text, "rate")
        if type(peak) is not bool:
            raise ValueError("provider peak must be a boolean")
        previous = right
    if previous is None or previous < end:
        raise ValueError("provider segments must cover the whole request")


def _account(rows, clipped):
    """Exact energy/cost/coverage, together with per-band aggregation."""
    energy = Fraction(0)
    cost = Fraction(0)
    # Observed seconds are the total length of the (disjoint) load rows clipped
    # to the request; splitting them against tariff cells must not change it.
    coverage = sum(right - left for left, right, _kw, _ident in rows)
    groups = {}
    i = j = 0
    while i < len(rows) and j < len(clipped):
        row_left, row_right, kw, _ident = rows[i]
        band_left, band_right, rate, name, _peak = clipped[j]
        left = max(row_left, band_left)
        right = min(row_right, band_right)
        if left < right:
            duration = right - left
            piece_energy = kw * duration / 3600
            piece_cost = piece_energy * rate
            energy += piece_energy
            cost += piece_cost
            group = groups.setdefault(name, [Fraction(0), Fraction(0)])
            group[0] += piece_energy
            group[1] += piece_cost
        if row_right <= band_right:
            i += 1
        if band_right <= row_right:
            j += 1
    return energy, cost, coverage, groups


def _build_load_curve(rows, start, end):
    """Cumulative observed-kWh and observed-second curves over [start, end].

    Gaps between rows (and uncovered ends) are genuine no-observation spans:
    the energy curve is flat there and the observed-seconds curve does not
    advance.  This is what lets the demand eligibility test distinguish a real
    zero-kW row from missing data.
    """
    times = [start]
    energy = [Fraction(0)]
    observed = [Fraction(0)]
    slopes = []
    is_observed = []

    cursor = start
    total_energy = Fraction(0)
    total_observed = Fraction(0)
    for left, right, kw, _ident in rows:
        if left > cursor:
            slopes.append(Fraction(0))
            is_observed.append(False)
            times.append(left)
            energy.append(total_energy)
            observed.append(total_observed)
        slopes.append(kw)
        is_observed.append(True)
        total_energy += kw * (right - left) / 3600
        total_observed += right - left
        times.append(right)
        energy.append(total_energy)
        observed.append(total_observed)
        cursor = right
    if cursor < end:
        slopes.append(Fraction(0))
        is_observed.append(False)
        times.append(end)
        energy.append(total_energy)
        observed.append(total_observed)

    return {
        "times": times,
        "energy": energy,
        "observed": observed,
        "slopes": slopes,
        "is_observed": is_observed,
    }


def _curve_position(curve, t):
    """Index of the knot at or before ``t`` and the integer offset from it."""
    index = bisect_right(curve["times"], t) - 1
    return index, t - curve["times"][index]


def _curve_at(curve, t, window):
    """(energy over [t, t+window), observed seconds over [t, t+window))."""
    start_energy = _value_at(curve, t)
    end_energy = _value_at(curve, t + window)
    start_obs = _observed_at(curve, t)
    end_obs = _observed_at(curve, t + window)
    return end_energy - start_energy, end_obs - start_obs


def _value_at(curve, t):
    times = curve["times"]
    if t <= times[0]:
        return curve["energy"][0]
    if t >= times[-1]:
        return curve["energy"][-1]
    index, offset = _curve_position(curve, t)
    return curve["energy"][index] + curve["slopes"][index] * offset / 3600


def _observed_at(curve, t):
    times = curve["times"]
    if t <= times[0]:
        return curve["observed"][0]
    if t >= times[-1]:
        return curve["observed"][-1]
    index, offset = _curve_position(curve, t)
    if curve["is_observed"][index]:
        return curve["observed"][index] + offset
    return curve["observed"][index]


def _build_peak_curve(clipped, start, end):
    """Cumulative peak seconds: knots plus the per-piece slope (0 or 1)."""
    times = [start]
    values = [Fraction(0)]
    slopes = []
    cursor = start
    total = Fraction(0)
    for left, right, _rate, _name, peak in clipped:
        if left > cursor:
            slopes.append(Fraction(0))
            times.append(left)
            values.append(total)
        slopes.append(Fraction(1) if peak else Fraction(0))
        total += (right - left) if peak else 0
        times.append(right)
        values.append(Fraction(total))
        cursor = right
    if cursor < end:
        slopes.append(Fraction(0))
        times.append(end)
        values.append(Fraction(total))
    return {"times": times, "values": values, "slopes": slopes}


def _peak_at(curve, t):
    times = curve["times"]
    if t <= times[0]:
        return curve["values"][0]
    if t >= times[-1]:
        return curve["values"][-1]
    index = bisect_right(times, t) - 1
    return curve["values"][index] + curve["slopes"][index] * (t - times[index])


def _peak_between(curve, start, end):
    return _peak_at(curve, end) - _peak_at(curve, start)
