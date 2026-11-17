"""Executable verification for the tariff catalogue and load warehouse.

Run with:  python3.11 materials/538/verify.py
Exit status is non-zero on the first failed check.
"""
from datetime import datetime, timezone
from fractions import Fraction
import json
import traceback

from tariff import TariffService
from load import LoadWarehouse


def utc(y, mo, d, h=0, mi=0, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp())


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def day(rows):
    return rows


def full_day(band="peak", rate="2", peak=True):
    return [{"start": "00:00", "end": "24:00", "band": band, "rate": rate, "peak": peak}]


def spec_utc(rows=None, rate="2", holidays=None):
    rows = rows or full_day("peak", rate, True)
    return {
        "timezone": "UTC",
        "weekdays": {str(d): rows for d in range(1, 8)},
        "holidays": holidays or {},
    }


FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}\n  got  {got!r}\n  want {want!r}")
        print(f"FAIL {name}: got {got!r} want {want!r}")
    else:
        print(f"ok   {name}")


def check_true(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name} {detail}")
        print(f"FAIL {name} {detail}")
    else:
        print(f"ok   {name}")


def band_entry(result, name):
    for entry in result["by_band"]:
        if entry["band"] == name:
            return entry
    raise AssertionError(f"band {name!r} missing from {result['by_band']}")


def raises(name, fn):
    try:
        fn()
    except ValueError:
        print(f"ok   {name} (raised ValueError)")
        return
    except Exception as exc:  # pragma: no cover - diagnostic
        FAILS.append(f"{name}: wrong exception {exc!r}")
        print(f"FAIL {name}: wrong exception {exc!r}")
        return
    FAILS.append(f"{name}: did not raise")
    print(f"FAIL {name}: did not raise")


# ---------------------------------------------------------------- tariff tests
def test_tariff_utc_regular():
    t0 = utc(2025, 1, 1)
    service = TariffService(spec_utc())
    segs = service.segments(t0, t0 + 3600)
    check("utc regular is one merged segment", segs,
          [{"start": t0, "end": t0 + 3600, "band": "peak", "rate": "2", "peak": True}])


def test_tariff_ny_spring_forward():
    # 2024-03-10: local 02:00 -> 03:00 skipped. Peak 00:00-03:00 local is
    # [05:00, 08:00) UTC?  No: 00:00 EST = 05:00Z, 03:00 EDT = 07:00Z, so
    # the whole peak wall span 00:00..02:00 (EST) plus the skipped hour is
    # only 2 real hours [05:00, 07:00).  The non-existent hour adds no UTC.
    rows = [
        {"start": "00:00", "end": "03:00", "band": "peak", "rate": "2", "peak": True},
        {"start": "03:00", "end": "24:00", "band": "off", "rate": "1", "peak": False},
    ]
    spec = {"timezone": "America/New_York",
            "weekdays": {str(d): rows for d in range(1, 8)}, "holidays": {}}
    service = TariffService(spec)
    start, end = utc(2024, 3, 10), utc(2024, 3, 11)
    segs = service.segments(start, end)
    # Seamless, no gaps, no overlaps.
    check_true("spring: seamless", all(a["end"] == b["start"] for a, b in zip(segs, segs[1:])))
    check("spring: covers request", (segs[0]["start"], segs[-1]["end"]), (start, end))
    peak = [s for s in segs if s["peak"]]
    check("spring: peak is exactly 2 real hours", [(s["start"], s["end"]) for s in peak],
          [(utc(2024, 3, 10, 5), utc(2024, 3, 10, 7))])
    # Query is one UTC calendar day (24h), independent of local day length.
    check("spring: request is a full UTC day", segs[-1]["end"] - segs[0]["start"], 24 * 3600)
    check("spring: 23h of local date lies in the request",
          sum(s["end"] - s["start"] for s in service.segments(utc(2024, 3, 10, 5),
                                                              utc(2024, 3, 11, 4))),
          23 * 3600)


def test_tariff_ny_fall_back():
    # 2024-11-03: 01:00-02:00 occurs twice.  Peak 01:00-01:30 must appear in
    # BOTH branches, and off resumes only after the second 01:30.
    rows = [
        {"start": "00:00", "end": "01:00", "band": "off", "rate": "1", "peak": False},
        {"start": "01:00", "end": "01:30", "band": "peak", "rate": "2", "peak": True},
        {"start": "01:30", "end": "24:00", "band": "off", "rate": "1", "peak": False},
    ]
    spec = {"timezone": "America/New_York",
            "weekdays": {str(d): rows for d in range(1, 8)}, "holidays": {}}
    service = TariffService(spec)
    start, end = utc(2024, 11, 3), utc(2024, 11, 4)
    segs = service.segments(start, end)
    check_true("fall: seamless", all(a["end"] == b["start"] for a, b in zip(segs, segs[1:])))
    check("fall: covers request", (segs[0]["start"], segs[-1]["end"]), (start, end))
    peak = [(s["start"], s["end"]) for s in segs if s["peak"]]
    check("fall: peak appears in both repeated branches", peak,
          [(utc(2024, 11, 3, 5), utc(2024, 11, 3, 5, 30)),
           (utc(2024, 11, 3, 6), utc(2024, 11, 3, 6, 30))])
    check("fall: day is 25h UTC", segs[-1]["end"] - segs[0]["start"], 24 * 3600)
    check("fall: 25h of local date lies in the request",
          sum(s["end"] - s["start"] for s in service.segments(utc(2024, 11, 3, 4),
                                                              utc(2024, 11, 4, 5))),
          25 * 3600)
    # Independently confirm the second branch: local 01:10 fold=1 is EST.
    moment = utc(2024, 11, 3, 6, 10)
    local = datetime.fromtimestamp(moment, service.zone)
    check("fall: local fold=1 01:10", (local.hour, local.minute, local.fold), (1, 10, 1))


def test_tariff_cross_midnight_holiday():
    weekday_rows = [
        {"start": "00:00", "end": "06:00", "band": "off", "rate": "1", "peak": False},
        {"start": "06:00", "end": "22:00", "band": "peak", "rate": "3", "peak": True},
        {"start": "22:00", "end": "24:00", "band": "off", "rate": "1", "peak": False},
    ]
    holiday_rows = [
        {"start": "00:00", "end": "12:00", "band": "holiday", "rate": "5", "peak": False},
        {"start": "12:00", "end": "24:00", "band": "holiday", "rate": "5", "peak": False},
    ]
    spec = {"timezone": "UTC",
            "weekdays": {str(d): weekday_rows for d in range(1, 8)},
            "holidays": {"2025-01-02": holiday_rows}}
    service = TariffService(spec)
    # Query spans the local midnight: 2025-01-01 late evening into 01-02.
    start, end = utc(2025, 1, 1, 21), utc(2025, 1, 2, 13)
    segs = service.segments(start, end)
    check("holiday: crosses midnight seamlessly",
          all(a["end"] == b["start"] for a, b in zip(segs, segs[1:])), True)
    midnight = utc(2025, 1, 2)
    before = [s for s in segs if s["end"] == midnight]
    after = [s for s in segs if s["start"] == midnight]
    check("holiday: previous day tail kept", before[0]["band"], "off")
    check("holiday: holiday replaces whole date", after[0]["band"], "holiday")
    hol = [s for s in segs if s["band"] == "holiday"]
    check("holiday: identical neighbours merged", len(hol), 1)
    check("holiday: starts at local midnight and clips at request end",
          (hol[0]["start"], hol[0]["end"]), (midnight, end))


def test_tariff_rate_not_from_name():
    # Same band name, different rate in different slices.
    rows = [
        {"start": "00:00", "end": "12:00", "band": "x", "rate": "1", "peak": False},
        {"start": "12:00", "end": "24:00", "band": "x", "rate": "3", "peak": False},
    ]
    service = TariffService(spec_utc(rows))
    segs = service.segments(utc(2025, 1, 1), utc(2025, 1, 2))
    check("rates follow slice, not name", [s["rate"] for s in segs], ["1", "3"])
    check("different rates not merged", len(segs), 2)


# ------------------------------------------------------------------ load tests
def test_prompt_primary_example():
    t0 = utc(2025, 1, 1)
    warehouse = LoadWarehouse()
    warehouse.put("m", [
        {"id": "a", "start": t0, "end": t0 + 400, "kw": "100"},
        {"id": "b", "start": t0 + 400, "end": t0 + 900, "kw": "0"},
        {"id": "c", "start": t0 + 900, "end": t0 + 1200, "kw": "100"},
    ])
    result = warehouse.query("m", t0, t0 + 1200, TariffService(spec_utc()), 900)
    check("primary: energy", result["energy_kwh"], "175/9")
    check("primary: cost", result["cost"], "350/9")
    check("primary: demand", result["demand_kw"], "400/9")
    check("primary: window", result["window"], [t0, t0 + 900])
    check("primary: sources", result["window_sources"], ["a", "b"])


def test_prompt_boundary_example():
    t0 = utc(2025, 1, 1)
    warehouse = LoadWarehouse()
    warehouse.put("m", [
        {"id": "a", "start": t0, "end": t0 + 400, "kw": "0"},
        {"id": "b", "start": t0 + 400, "end": t0 + 1500, "kw": "100"},
        {"id": "c", "start": t0 + 1500, "end": t0 + 1800, "kw": "0"},
    ])
    result = warehouse.query("m", t0, t0 + 1800, TariffService(spec_utc()), 900)
    check("boundary: earliest max window ends mid-row", result["window"],
          [t0 + 400, t0 + 1300])
    check("boundary: demand", result["demand_kw"], "100")


def test_peak_coverage_short():
    t0 = utc(2025, 1, 1)
    rows = [
        {"start": "00:00", "end": "00:10", "band": "peak", "rate": "2", "peak": True},
        {"start": "00:10", "end": "24:00", "band": "off", "rate": "1", "peak": False},
    ]
    warehouse = LoadWarehouse()
    warehouse.put("m", [{"id": "a", "start": t0, "end": t0 + 1200, "kw": "100"}])
    result = warehouse.query("m", t0, t0 + 1200, TariffService(spec_utc(rows)), 900)
    check("peak-only-600s: no qualifying window", result["demand_kw"], None)
    check("peak-only-600s: window null", result["window"], None)
    check("peak-only-600s: sources empty", result["window_sources"], [])


def test_zero_power_full_window():
    t0 = utc(2025, 1, 1)
    warehouse = LoadWarehouse()
    warehouse.put("m", [{"id": "z", "start": t0, "end": t0 + 1200, "kw": "0"}])
    result = warehouse.query("m", t0, t0 + 1200, TariffService(spec_utc()), 900)
    check("zero: demand is 0 not null", result["demand_kw"], "0")
    check("zero: window", result["window"], [t0, t0 + 900])
    check("zero: sources include zero row", result["window_sources"], ["z"])
    check("zero: energy", result["energy_kwh"], "0")
    check("zero: by_band keeps band", [b["band"] for b in result["by_band"]], ["peak"])


def test_gap_and_missing():
    t0 = utc(2025, 1, 1)
    warehouse = LoadWarehouse()
    # 300s observed, 300s gap, 300s observed, 300s gap within 1200s query.
    warehouse.put("m", [
        {"id": "a", "start": t0, "end": t0 + 300, "kw": "60"},
        {"id": "b", "start": t0 + 600, "end": t0 + 900, "kw": "60"},
    ])
    result = warehouse.query("m", t0, t0 + 1200, TariffService(spec_utc()), 300)
    check("gap: coverage", result["coverage_seconds"], 600)
    check("gap: missing", result["missing_seconds"], 600)
    check("gap: energy only observed", result["energy_kwh"], str(Fraction(60 * 600, 3600)))
    # Windows crossing a gap are ineligible: only fully-observed 300s windows
    # are [0,300) and [600,900); both average 60. Earliest is [0,300).
    check("gap: earliest full window", result["window"], [t0, t0 + 300])
    check("gap: demand", result["demand_kw"], "60")


def test_tariff_boundary_splits_cost():
    t0 = utc(2025, 1, 1)
    rows = [
        {"start": "00:00", "end": "00:20", "band": "peak", "rate": "2", "peak": True},
        {"start": "00:20", "end": "24:00", "band": "off", "rate": "1", "peak": False},
    ]
    warehouse = LoadWarehouse()
    # One load row spans the tariff boundary at +1200s.
    warehouse.put("m", [{"id": "a", "start": t0, "end": t0 + 1800, "kw": "100"}])
    result = warehouse.query("m", t0, t0 + 1800, TariffService(spec_utc(rows)), 900)
    # peak 1200s @100kW => 100/3 kWh * 2 ; off 600s => 50/3 kWh * 1
    check("split: peak energy", result["by_band"][1]["energy_kwh"], "100/3")
    check("split: peak cost", result["by_band"][1]["cost"], "200/3")
    check("split: off energy", result["by_band"][0]["energy_kwh"], "50/3")
    check("split: off cost", result["by_band"][0]["cost"], "50/3")
    check("split: total cost", result["cost"], "250/3")
    check("split: total energy", result["energy_kwh"], "50")
    # Demand window may not cross into the non-peak slice at all.
    check("split: only peak window candidate", result["window"], [t0, t0 + 900])


def test_same_band_merges_in_report():
    t0 = utc(2025, 1, 1)
    rows = [
        {"start": "00:00", "end": "00:20", "band": "grid", "rate": "2", "peak": True},
        {"start": "00:20", "end": "24:00", "band": "grid", "rate": "5", "peak": False},
    ]
    warehouse = LoadWarehouse()
    warehouse.put("m", [{"id": "a", "start": t0, "end": t0 + 1800, "kw": "100"}])
    result = warehouse.query("m", t0, t0 + 1800, TariffService(spec_utc(rows)), 900)
    check("same band: one report group", len(result["by_band"]), 1)
    check("same band: energy summed", result["by_band"][0]["energy_kwh"], "50")
    # 20min@2 + 10min@5 on 100kW: 100/3*2 + 50/3*5 = 200/3 + 250/3 = 150.
    check("same band: cost summed", result["by_band"][0]["cost"], "150")


def test_external_provider():
    t0 = utc(2025, 1, 1)

    class Foreign:
        def segments(self, start, end):
            # Arbitrary non-aligned boundaries and a different band vocabulary.
            return [
                {"start": start, "end": start + 137, "band": "A", "rate": "7/3", "peak": True},
                {"start": start + 137, "end": start + 1000, "band": "B", "rate": "1/2", "peak": False},
                {"start": start + 1000, "end": end, "band": "A", "rate": "7/3", "peak": True},
            ]

    warehouse = LoadWarehouse()
    warehouse.put("m", [{"id": "a", "start": t0, "end": t0 + 1800, "kw": "100"}])
    result = warehouse.query("m", t0, t0 + 1800, Foreign(), 900)
    # A slice: 137s + 800s = 937s ; B: 863s
    e_a = Fraction(100 * 937, 3600)
    e_b = Fraction(100 * 863, 3600)
    check("external: total energy", result["energy_kwh"], str(e_a + e_b))
    check("external: A group energy", result["by_band"][0]["energy_kwh"], str(e_a))
    check("external: cost exact", result["cost"], str(e_a * Fraction(7, 3) + e_b * Fraction(1, 2)))
    # No 900s all-peak window: peak only [0,137) and [1000,1800), separated by B.
    check("external: no demand window", result["demand_kw"], None)


def test_external_snapshot_used_once():
    t0 = utc(2025, 1, 1)

    class Counting:
        def __init__(self):
            self.calls = 0

        def segments(self, start, end):
            self.calls += 1
            return [{"start": start, "end": end, "band": "p", "rate": "1", "peak": True}]

    provider = Counting()
    warehouse = LoadWarehouse()
    warehouse.put("m", [{"id": "a", "start": t0, "end": t0 + 3600, "kw": "10"}])
    warehouse.query("m", t0, t0 + 3600, provider, 900)
    check("external: segments called exactly once", provider.calls, 1)


def test_tie_earliest():
    t0 = utc(2025, 1, 1)
    warehouse = LoadWarehouse()
    # Two equal maxima separated by a lower stretch; earliest must win.
    warehouse.put("m", [
        {"id": "a", "start": t0, "end": t0 + 900, "kw": "10"},
        {"id": "b", "start": t0 + 900, "end": t0 + 1800, "kw": "1"},
        {"id": "c", "start": t0 + 1800, "end": t0 + 2700, "kw": "10"},
    ])
    result = warehouse.query("m", t0, t0 + 2700, TariffService(spec_utc()), 900)
    check("tie: earliest window", result["window"], [t0, t0 + 900])
    check("tie: demand", result["demand_kw"], "10")


def test_atomic_put_and_unknown_series():
    warehouse = LoadWarehouse()
    t0 = utc(2025, 1, 1)
    warehouse.put("m", [{"id": "a", "start": t0, "end": t0 + 100, "kw": "5"}])
    raises("atomic: overlapping replacement rejected",
           lambda: warehouse.put("m", [
               {"id": "x", "start": t0, "end": t0 + 200, "kw": "1"},
               {"id": "y", "start": t0 + 100, "end": t0 + 300, "kw": "2"},
           ]))
    result = warehouse.query("m", t0, t0 + 100, TariffService(spec_utc()), 50)
    check("atomic: original preserved", result["energy_kwh"], str(Fraction(5 * 100, 3600)))
    raises("atomic: duplicate id rejected",
           lambda: warehouse.put("m", [
               {"id": "d", "start": t0, "end": t0 + 100, "kw": "1"},
               {"id": "d", "start": t0 + 100, "end": t0 + 200, "kw": "2"},
           ]))
    raises("atomic: negative kw rejected",
           lambda: warehouse.put("m", [{"id": "n", "start": t0, "end": t0 + 1, "kw": "-1"}]))
    raises("atomic: non-int start rejected",
           lambda: warehouse.put("m", [{"id": "f", "start": t0 + 0.5, "end": t0 + 2, "kw": "1"}]))

    empty = warehouse.query("never-seen", t0, t0 + 900, TariffService(spec_utc()), 100)
    check("unknown series: empty energy", empty["energy_kwh"], "0")
    check("unknown series: full missing", empty["missing_seconds"], 900)
    check("unknown series: no demand", empty["window"], None)
    check("unknown series: empty by_band", empty["by_band"], [])


def test_invalid_inputs():
    t0 = utc(2025, 1, 1)
    raises("spec: missing weekday", lambda: TariffService(
        {"timezone": "UTC", "weekdays": {"1": full_day()}}))
    raises("spec: bad timezone", lambda: TariffService(
        {"timezone": "Not/AZone", "weekdays": {str(d): full_day() for d in range(1, 8)}}))
    raises("spec: incomplete day", lambda: TariffService(spec_utc(
        [{"start": "00:00", "end": "12:00", "band": "p", "rate": "1", "peak": True}])))
    raises("spec: gap in day", lambda: TariffService(spec_utc([
        {"start": "00:00", "end": "12:00", "band": "p", "rate": "1", "peak": True},
        {"start": "13:00", "end": "24:00", "band": "o", "rate": "1", "peak": False}])))
    raises("spec: negative rate", lambda: TariffService(spec_utc(
        [{"start": "00:00", "end": "24:00", "band": "p", "rate": "-1", "peak": True}])))
    raises("spec: non-bool peak", lambda: TariffService(spec_utc(
        [{"start": "00:00", "end": "24:00", "band": "p", "rate": "1", "peak": 1}])))
    raises("spec: bad holiday key", lambda: TariffService(
        {"timezone": "UTC", "weekdays": {str(d): full_day() for d in range(1, 8)},
         "holidays": {"2025-13-01": full_day()}}))
    service = TariffService(spec_utc())
    raises("segments: reversed range", lambda: service.segments(t0, t0))
    raises("segments: float seconds", lambda: service.segments(t0, t0 + 1.5))

    warehouse = LoadWarehouse()
    warehouse.put("m", [{"id": "a", "start": t0, "end": t0 + 100, "kw": "1"}])
    raises("query: reversed range", lambda: warehouse.query("m", t0 + 10, t0, service))
    raises("query: zero window", lambda: warehouse.query("m", t0, t0 + 10, service, 0))
    raises("query: float window", lambda: warehouse.query("m", t0, t0 + 10, service, 1.5))


def test_utc_regular_example():
    """Preserve the original ordinary UTC / equal-five-minute example."""
    t0 = utc(2025, 1, 1)
    rows = [{"start": "00:00", "end": "00:15", "band": "peak", "rate": "2", "peak": True},
            {"start": "00:15", "end": "24:00", "band": "off", "rate": "1", "peak": False}]
    warehouse = LoadWarehouse()
    warehouse.put("meter-A", [
        {"id": str(i), "start": t0 + i * 300, "end": t0 + (i + 1) * 300,
         "kw": "60" if i < 3 else "0"} for i in range(6)])
    result = warehouse.query("meter-A", t0, t0 + 1800, TariffService(spec_utc(rows)))
    check("regular: energy", result["energy_kwh"], "15")
    check("regular: cost", result["cost"], "30")
    check("regular: coverage", result["coverage_seconds"], 1800)
    # First 900s all peak, all fully observed -> demand 60.
    check("regular: demand", result["demand_kw"], "60")
    check("regular: window", result["window"], [t0, t0 + 900])


def test_ny_rollover_with_load():
    """Identical load facts produce one continuous UTC interpretation across DST."""
    rows = [
        {"start": "00:00", "end": "06:00", "band": "off", "rate": "1", "peak": False},
        {"start": "06:00", "end": "22:00", "band": "peak", "rate": "2", "peak": True},
        {"start": "22:00", "end": "24:00", "band": "off", "rate": "1", "peak": False},
    ]
    spec = {"timezone": "America/New_York",
            "weekdays": {str(d): rows for d in range(1, 8)}, "holidays": {}}
    service = TariffService(spec)
    start, end = utc(2024, 11, 3), utc(2024, 11, 4)
    warehouse = LoadWarehouse()
    warehouse.put("m", [{"id": "all", "start": start, "end": end, "kw": "10"}])
    result = warehouse.query("m", start, end, service, 900)
    total = sum(s["end"] - s["start"] for s in service.segments(start, end))
    check("rollover: coverage equals requested span", result["coverage_seconds"], total)
    expected_energy = Fraction(10 * total, 3600)
    check("rollover: exact energy", result["energy_kwh"], str(expected_energy))
    # Query spans 20:00 Nov 2 EDT .. 19:00 Nov 3 EST.  Peak is the last 2h of
    # Nov 2 (20:00-22:00) plus 06:00-19:00 on Nov 3 = 13h, so 15 real hours.
    peak_hours = sum(
        s["end"] - s["start"] for s in service.segments(start, end) if s["peak"])
    check("rollover: peak real hours", peak_hours, 15 * 3600)
    check("rollover: peak energy", band_entry(result, "peak")["energy_kwh"],
          str(Fraction(10 * peak_hours, 3600)))
    check("rollover: demand valid", result["demand_kw"] is not None, True)


def test_randomized_differential():
    """Cross-check the interval algorithm against a per-second brute force."""
    import random

    def brute(rows, bands, start, end, w):
        power = [None] * (end - start)
        peak = [False] * (end - start)
        for l, r, kw, _i in rows:
            for t in range(max(l, start), min(r, end)):
                power[t - start] = kw
        for l, r, _rt, _n, pk in bands:
            for t in range(max(l, start), min(r, end)):
                peak[t - start] = pk
        best = best_s = None
        for s in range(0, end - start - w + 1):
            span = range(s, s + w)
            if any(power[i] is None for i in span) or not all(peak[i] for i in span):
                continue
            energy = sum(power[i] for i in span) / Fraction(3600)
            if best is None or energy > best:
                best, best_s = energy, s
        if best is None:
            return None, None
        return str(best * 3600 / w), [start + best_s, start + best_s + w]

    rng = random.Random(20240538)
    checked = 0
    for _ in range(300):
        t0 = 1700000000
        total = rng.randint(20, 240)
        cuts = sorted(rng.sample(range(0, total + 1), k=rng.randint(2, 6)))
        rows = []
        for index, (a, b) in enumerate(zip(cuts, cuts[1:])):
            if b > a and rng.random() > 0.3:
                rows.append((t0 + a, t0 + b, Fraction(rng.randint(0, 20)), f"r{index}"))
        edges = [0] + sorted(rng.sample(range(1, total), k=rng.randint(0, 3))) + [total]
        bands, cursor = [], 0
        for a, b in zip(edges, edges[1:]):
            if b > a:
                bands.append((t0 + a, t0 + b, Fraction(rng.randint(0, 4)), "x",
                              rng.random() < 0.6))
                cursor = b
        if cursor < total:
            bands.append((t0 + cursor, t0 + total, Fraction(0), "x", False))
        w = rng.choice([1, 2, 3, 5, 10, 30, 60, 100])
        if w > total:
            continue
        checked += 1

        warehouse = LoadWarehouse()
        warehouse.put("m", [{"id": i, "start": l, "end": r, "kw": str(k)}
                            for l, r, k, i in rows])

        class Foreign:
            def segments(self, s, e):
                return [{"start": l, "end": r, "band": n, "rate": str(rt), "peak": p}
                        for l, r, rt, n, p in bands]

        result = warehouse.query("m", t0, t0 + total, Foreign(), w)
        want_demand, want_window = brute(rows, bands, t0, t0 + total, w)
        if (result["demand_kw"], result["window"]) != (want_demand, want_window):
            check(f"random case w={w}", (result["demand_kw"], result["window"]),
                  (want_demand, want_window))
            return
    check_true("randomized differential agrees", checked >= 250, f"(checked {checked})")


def test_randomized_tariff_projection():
    """Cross-check UTC segmentation against per-second local classification."""
    import random
    from zoneinfo import ZoneInfo

    rng = random.Random(538)

    def random_day():
        edges = [0] + sorted(rng.sample(range(1, 1440), k=rng.randint(0, 4))) + [1440]
        return [
            {"start": f"{a // 60:02d}:{a % 60:02d}", "end": f"{b // 60:02d}:{b % 60:02d}",
             "band": rng.choice(["p", "o", "h"]),
             "rate": str(rng.choice([0, 1, 2, 3])), "peak": rng.random() < 0.5}
            for a, b in zip(edges, edges[1:])
        ]

    zones = ["America/New_York", "Europe/London", "Australia/Lord_Howe", "UTC",
             "America/Santiago"]
    bases = [1710000000, 1729900000, 1717200000, 1730600000]
    checked = 0
    for _ in range(40):
        zone = rng.choice(zones)
        raw_days = {d: random_day() for d in range(1, 8)}
        raw_holidays = {
            f"2024-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}": random_day()
            for _ in range(2)
        }
        spec = {"timezone": zone, "weekdays": raw_days, "holidays": raw_holidays}
        service = TariffService(spec)
        base = rng.choice(bases)
        start = base
        end = base + rng.randint(1, 3 * 86400)
        segments = service.segments(start, end)
        if segments[0]["start"] != start or segments[-1]["end"] != end:
            check_true("random tariff covers request", False)
            return
        if any(a["end"] != b["start"] for a, b in zip(segments, segments[1:])):
            check_true("random tariff seamless", False)
            return
        zone_info = ZoneInfo(zone)

        def classify(t):
            local = datetime.fromtimestamp(t, zone_info)
            minute = local.hour * 60 + local.minute
            table = raw_holidays.get(local.date().isoformat(),
                                     raw_days[local.isoweekday()])
            for row in table:
                a = int(row["start"][:2]) * 60 + int(row["start"][3:])
                b = int(row["end"][:2]) * 60 + int(row["end"][3:])
                if a <= minute < b:
                    return row["band"], str(Fraction(row["rate"])), row["peak"]
            raise AssertionError("uncovered local minute")

        for segment in segments:
            for t in {segment["start"], (segment["start"] + segment["end"]) // 2,
                      segment["end"] - 1}:
                got = (segment["band"], segment["rate"], segment["peak"])
                if got != classify(t):
                    check(f"random tariff {zone} @ {iso(t)}", got, classify(t))
                    return
        checked += 1
    check_true("randomized tariff projection agrees", checked == 40, f"(checked {checked})")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        print(f"\n== {test.__name__} ==")
        try:
            test()
        except Exception:
            FAILS.append(f"{test.__name__} raised:\n{traceback.format_exc()}")
            print(traceback.format_exc())
    print("\n" + "=" * 60)
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S)")
        for item in FAILS:
            print(" -", item.splitlines()[0])
        raise SystemExit(1)
    print(f"All {len(tests)} test groups passed.")


if __name__ == "__main__":
    main()
