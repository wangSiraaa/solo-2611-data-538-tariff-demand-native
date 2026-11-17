"""Demonstrate the ordinary UTC example and the two fixed deviations.

Run with:  python3.11 materials/538/probe.py
"""
from datetime import datetime, timezone
from fractions import Fraction
import json

from tariff import TariffService
from load import LoadWarehouse


def utc(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp())


def spec(rate="2"):
    rows = [{"start": "00:00", "end": "00:15", "band": "peak", "rate": rate, "peak": True},
            {"start": "00:15", "end": "24:00", "band": "off", "rate": "1", "peak": False}]
    return {"timezone": "UTC", "weekdays": {str(day): rows for day in range(1, 8)}, "holidays": {}}


def normal():
    t = utc(2025, 1, 1)
    warehouse = LoadWarehouse()
    warehouse.put("meter-A", [{"id": str(i), "start": t + i * 300, "end": t + (i + 1) * 300,
                              "kw": "60" if i < 3 else "0"} for i in range(6)])
    return warehouse.query("meter-A", t, t + 1800, TariffService(spec()))


def new_york_repeated_hour():
    """The second half of the repeated 01:00 hour still bills as peak."""
    rows = [
        {"start": "00:00", "end": "01:00", "band": "off", "rate": "1", "peak": False},
        {"start": "01:00", "end": "01:30", "band": "peak", "rate": "2", "peak": True},
        {"start": "01:30", "end": "24:00", "band": "off", "rate": "1", "peak": False},
    ]
    spec_ny = {"timezone": "America/New_York",
               "weekdays": {str(d): rows for d in range(1, 8)}, "holidays": {}}
    service = TariffService(spec_ny)
    start, end = utc(2024, 11, 3), utc(2024, 11, 4)
    segments = service.segments(start, end)
    return {
        "utc_day_seconds": end - start,
        "peak_utc_offsets": [
            [s["start"] - start, s["end"] - start] for s in segments if s["peak"]
        ],
        "both_branches_present": len([s for s in segments if s["peak"]]) == 2,
    }


def irregular_load():
    """400s@100kW, 500s@0kW, 300s@100kW -> exact energy/cost/demand."""
    t = utc(2025, 1, 1)
    rows = [{"start": "00:00", "end": "24:00", "band": "peak", "rate": "2", "peak": True}]
    spec_utc = {"timezone": "UTC",
                "weekdays": {str(d): rows for d in range(1, 8)}, "holidays": {}}
    warehouse = LoadWarehouse()
    warehouse.put("meter-B", [
        {"id": "a", "start": t, "end": t + 400, "kw": "100"},
        {"id": "b", "start": t + 400, "end": t + 900, "kw": "0"},
        {"id": "c", "start": t + 900, "end": t + 1200, "kw": "100"},
    ])
    return warehouse.query("meter-B", t, t + 1200, TariffService(spec_utc), 900)


if __name__ == "__main__":
    print("ordinary UTC/equal-five-minute:", json.dumps(normal(), sort_keys=True))
    print("New York repeated hour:       ", json.dumps(new_york_repeated_hour(), sort_keys=True))
    print("irregular load + demand:      ", json.dumps(irregular_load(), sort_keys=True))
