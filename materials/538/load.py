"""Load facts and the regular-sample demand report."""
from copy import deepcopy
from fractions import Fraction


class LoadWarehouse:
    def __init__(self):
        self.series = {}

    def put(self, series, rows):
        saved = []
        seen = set()
        for row in sorted(rows, key=lambda r: (r["start"], r["end"])):
            start, end = row["start"], row["end"]
            kw = Fraction(row["kw"])
            if (not isinstance(start, int) or not isinstance(end, int) or start >= end
                    or kw < 0 or row["id"] in seen or (saved and start < saved[-1]["end"])):
                raise ValueError("invalid load facts")
            seen.add(row["id"])
            saved.append({"id": row["id"], "start": start, "end": end, "kw": str(kw)})
        self.series[series] = saved

    def query(self, series, start, end, provider, window_seconds=900):
        if not isinstance(start, int) or not isinstance(end, int) or start >= end or not isinstance(window_seconds, int) or window_seconds <= 0:
            raise ValueError("invalid query")
        bands = deepcopy(provider.segments(start, end))
        def band_at(t):
            return next(row for row in bands if row["start"] <= t < row["end"])
        rows = [{**row, "start": max(row["start"], start), "end": min(row["end"], end)}
                for row in self.series.get(series, []) if row["start"] < end and row["end"] > start]
        energy, cost, coverage = Fraction(0), Fraction(0), 0
        buckets = {}
        for row in rows:
            band = band_at(row["start"])
            e = Fraction(row["kw"]) * (row["end"] - row["start"]) / 3600
            c = e * Fraction(band["rate"])
            energy += e
            cost += c
            coverage += row["end"] - row["start"]
            group = buckets.setdefault(band["band"], [Fraction(0), Fraction(0)])
            group[0] += e
            group[1] += c
        best, chosen, sources = None, None, []
        if rows:
            n = max(1, window_seconds // (rows[0]["end"] - rows[0]["start"]))
            for j in range(n - 1, len(rows)):
                selection = rows[j - n + 1:j + 1]
                right = rows[j]["end"]
                left = right - window_seconds
                if left < start or not band_at(right - 1)["peak"]:
                    continue
                value = sum((Fraction(row["kw"]) for row in selection), Fraction(0)) / n
                if best is None or value > best:
                    best, chosen, sources = value, [left, right], [row["id"] for row in selection]
        return {"energy_kwh": str(energy), "cost": str(cost),
                "by_band": [{"band": key, "energy_kwh": str(value[0]), "cost": str(value[1])} for key, value in sorted(buckets.items())],
                "coverage_seconds": coverage, "missing_seconds": end - start - coverage,
                "demand_kw": None if best is None else str(best), "window": chosen, "window_sources": sources}
