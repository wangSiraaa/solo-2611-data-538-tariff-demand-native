"""Daily-band catalogue used by the fixed-offset deployment."""
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from zoneinfo import ZoneInfo


def parse_day(rows):
    output = []
    frontier = 0
    for row in rows:
        def minute(value):
            h, m = map(int, value.split(":"))
            if not (0 <= m < 60 and 0 <= h <= 24 and (h < 24 or m == 0)):
                raise ValueError("invalid clock")
            return h * 60 + m
        a, b = minute(row["start"]), minute(row["end"])
        rate = Fraction(row["rate"])
        if a != frontier or not a < b <= 1440 or rate < 0 or not isinstance(row["peak"], bool):
            raise ValueError("daily bands must cover one complete day")
        output.append({"a": a, "b": b, "rate": str(rate), "band": str(row["band"]), "peak": row["peak"]})
        frontier = b
    if frontier != 1440:
        raise ValueError("incomplete day")
    return output


class TariffService:
    def __init__(self, spec):
        self.zone = ZoneInfo(spec["timezone"])
        self.weekdays = {int(k): parse_day(v) for k, v in spec["weekdays"].items()}
        if set(self.weekdays) != set(range(1, 8)):
            raise ValueError("all weekdays required")
        self.holidays = {k: parse_day(v) for k, v in spec.get("holidays", {}).items()}
        for value in self.holidays:
            datetime.strptime(value, "%Y-%m-%d")

    def segments(self, start, end):
        if not isinstance(start, int) or not isinstance(end, int) or start >= end:
            raise ValueError("invalid range")
        first = datetime.fromtimestamp(start, self.zone).date() - timedelta(days=1)
        last = datetime.fromtimestamp(end, self.zone).date() + timedelta(days=1)
        output = []
        day = first
        while day <= last:
            rows = self.holidays.get(day.isoformat(), self.weekdays[day.isoweekday()])
            midnight = datetime(day.year, day.month, day.day)
            for row in rows:
                left = int((midnight + timedelta(minutes=row["a"])).replace(tzinfo=self.zone).timestamp())
                right = int((midnight + timedelta(minutes=row["b"])).replace(tzinfo=self.zone).timestamp())
                a, b = max(left, start), min(right, end)
                if a < b:
                    output.append({"start": a, "end": b, "rate": row["rate"], "band": row["band"], "peak": row["peak"]})
            day += timedelta(days=1)
        return sorted(output, key=lambda row: (row["start"], row["end"]))
