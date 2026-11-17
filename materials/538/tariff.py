"""Daily-band tariff catalogue.

A catalogue is defined entirely by local wall-clock rules.  ``segments`` is the
only way a consumer sees it: it projects those rules onto real UTC time.  The
projection is done by finding the UTC instants at which the local
classification can change (local day/segment boundaries and zone transitions)
and then classifying the interior of every resulting interval.  Attaching a
``tzinfo`` to a local endpoint is deliberately avoided: it is wrong for the
repeated and skipped local hours of a DST transition.
"""
from bisect import bisect_right
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from fractions import Fraction
import re
from zoneinfo import ZoneInfo

_DAY_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _minute_of_day(value):
    """Parse ``HH:MM`` into minutes since local midnight (0..1440)."""
    if not isinstance(value, str):
        raise ValueError("clock value must be a string")
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError("clock value must be HH:MM")
    hh, mm = parts
    if not (hh.isdigit() and mm.isdigit()) or len(hh) not in (1, 2) or len(mm) != 2:
        raise ValueError("clock value must be HH:MM")
    hour, minute = int(hh), int(mm)
    if not (0 <= minute < 60 and 0 <= hour <= 24):
        raise ValueError("clock value out of range")
    if hour == 24 and minute != 0:
        raise ValueError("clock value out of range")
    return hour * 60 + minute


def parse_day(rows):
    """Validate and canonicalise one local-day table."""
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError("a day table must be a non-empty sequence of segments")
    output = []
    frontier = 0
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("each segment must be a mapping")
        try:
            band = row["band"]
            rate_text = row["rate"]
            peak = row["peak"]
            a = _minute_of_day(row["start"])
            b = _minute_of_day(row["end"])
        except KeyError as exc:
            raise ValueError("segment is missing a required field") from exc

        if not isinstance(band, str) or band == "":
            raise ValueError("band must be a non-empty string")
        if not isinstance(rate_text, str) or rate_text == "":
            raise ValueError("rate must be a non-empty rational string")
        try:
            rate = Fraction(rate_text)
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError("rate must be a rational number") from exc
        if rate < 0:
            raise ValueError("rate must be non-negative")
        if type(peak) is not bool:
            raise ValueError("peak must be a boolean")

        # Left-closed, right-open, non-empty, and contiguous from 00:00.
        if a != frontier or not (a < b <= 1440):
            raise ValueError("daily bands must cover one complete day")
        output.append(
            {
                "a": a,
                "b": b,
                "band": band,
                "rate": rate,
                "rate_text": str(rate),
                "peak": peak,
            }
        )
        frontier = b
    if frontier != 1440:
        raise ValueError("daily bands must cover one complete day")
    return output


class TariffService:
    """Independent local-time tariff catalogue exposed as UTC segments."""

    # Zone offsets can change at most a handful of times per query in the
    # supported 2000..2035 range.  Hourly probing plus a binary search finds
    # the exact transition second without depending on zoneinfo internals.
    _SCAN_STEP = 3600

    def __init__(self, spec):
        if not isinstance(spec, Mapping):
            raise ValueError("spec must be a mapping")
        tz_name = spec.get("timezone")
        if not isinstance(tz_name, str) or tz_name == "":
            raise ValueError("spec.timezone must be an IANA timezone name")
        try:
            self.zone = ZoneInfo(tz_name)
        except Exception as exc:  # ZoneInfoNotFoundError, ValueError, ...
            raise ValueError("unsupported timezone") from exc

        weekdays = spec.get("weekdays")
        if not isinstance(weekdays, Mapping):
            raise ValueError("spec.weekdays must be a mapping")
        parsed_weekdays = {}
        for key, value in weekdays.items():
            try:
                number = int(key)
            except (TypeError, ValueError) as exc:
                raise ValueError("weekday keys must be ISO weekday numbers") from exc
            if number in parsed_weekdays:
                raise ValueError("duplicate weekday")
            parsed_weekdays[number] = parse_day(value)
        if set(parsed_weekdays) != set(range(1, 8)):
            raise ValueError("weekdays must cover ISO weekdays 1..7")
        self.weekdays = parsed_weekdays

        holidays = spec.get("holidays")
        if holidays is None:
            holidays = {}
        if not isinstance(holidays, Mapping):
            raise ValueError("spec.holidays must be a mapping")
        parsed_holidays = {}
        for key, value in holidays.items():
            if not isinstance(key, str) or not _DAY_RE.match(key):
                raise ValueError("holiday keys must be YYYY-MM-DD")
            try:
                day = date.fromisoformat(key)
            except ValueError as exc:
                raise ValueError("holiday key is not a calendar date") from exc
            parsed_holidays[day] = parse_day(value)
        self.holidays = parsed_holidays

    # ------------------------------------------------------------------ rules
    def _table_for(self, day):
        table = self.holidays.get(day)
        if table is not None:
            return table
        return self.weekdays[day.isoweekday()]

    def _classify(self, timestamp):
        """Band/rate/peak in force at one real UTC instant."""
        local = datetime.fromtimestamp(timestamp, self.zone)
        minute = local.hour * 60 + local.minute
        for row in self._table_for(local.date()):
            if row["a"] <= minute < row["b"]:
                return row["band"], row["rate"], row["peak"]
        # A complete day table always covers every local minute.
        raise ValueError("local time is not covered by the day table")

    # ---------------------------------------------------------------- zone ops
    def _offset(self, timestamp):
        return datetime.fromtimestamp(timestamp, self.zone).utcoffset()

    def _transition_points(self, start, end):
        """UTC instants strictly inside (start, end) where the offset changes."""
        points = []
        t = start
        previous = self._offset(t)
        while t < end:
            nxt = min(t + self._SCAN_STEP, end)
            current = self._offset(nxt)
            if current != previous:
                lo, hi = t, nxt
                # offset(lo) == previous and offset(hi) != previous
                while lo + 1 < hi:
                    mid = (lo + hi) // 2
                    if self._offset(mid) == previous:
                        lo = mid
                    else:
                        hi = mid
                points.append(hi)
            previous = current
            t = nxt
        return points

    def _utc_instants(self, naive_local):
        """All real UTC seconds displaying ``naive_local``.

        Empty for a skipped local time; two entries for an ambiguous one.
        """
        result = []
        for fold in (0, 1):
            aware = naive_local.replace(tzinfo=self.zone, fold=fold)
            timestamp = int(aware.timestamp())
            # Reject imaginary values: the round trip must reproduce the wall
            # clock.  This is the check that simply attaching tzinfo omits.
            back = datetime.fromtimestamp(timestamp, self.zone).replace(tzinfo=None)
            if back == naive_local and timestamp not in result:
                result.append(timestamp)
        return result

    # ------------------------------------------------------------------ public
    def segments(self, start, end):
        if not _is_int(start) or not _is_int(end) or start >= end:
            raise ValueError("segments() needs integer start < end")

        first = datetime.fromtimestamp(start, self.zone).date() - timedelta(days=2)
        last = datetime.fromtimestamp(end, self.zone).date() + timedelta(days=2)

        candidates = {start, end}
        day = first
        while day <= last:
            table = self._table_for(day)
            minutes = set()
            for row in table:
                minutes.add(row["a"])
                minutes.add(row["b"])
            base = datetime.combine(day, time.min)
            for minute in minutes:
                naive = base + timedelta(minutes=minute)
                for timestamp in self._utc_instants(naive):
                    if start < timestamp < end:
                        candidates.add(timestamp)
            day += timedelta(days=1)

        for timestamp in self._transition_points(start, end):
            if start < timestamp < end:
                candidates.add(timestamp)

        points = sorted(candidates)
        output = []
        for left, right in zip(points, points[1:]):
            # The classification is constant on the open interval: every local
            # boundary and every offset jump is a candidate point.
            midpoint = (left + right) // 2
            band, rate, peak = self._classify(midpoint)
            if (
                output
                and output[-1]["end"] == left
                and output[-1]["band"] == band
                and output[-1]["rate"] == str(rate)
                and output[-1]["peak"] == peak
            ):
                output[-1]["end"] = right
            else:
                output.append(
                    {
                        "start": left,
                        "end": right,
                        "band": band,
                        "rate": str(rate),
                        "peak": peak,
                    }
                )
        return output
