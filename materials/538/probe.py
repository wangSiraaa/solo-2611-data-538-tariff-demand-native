"""Run the ordinary UTC, equal-five-minute deployment example."""
import json
from tariff import TariffService
from load import LoadWarehouse


def spec(rate="2"):
    rows = [{"start": "00:00", "end": "00:15", "band": "peak", "rate": rate, "peak": True},
            {"start": "00:15", "end": "24:00", "band": "off", "rate": "1", "peak": False}]
    return {"timezone": "UTC", "weekdays": {str(day): rows for day in range(1, 8)}, "holidays": {}}


def normal():
    t = 1735689600
    warehouse = LoadWarehouse()
    warehouse.put("meter-A", [{"id": str(i), "start": t + i * 300, "end": t + (i + 1) * 300,
                              "kw": "60" if i < 3 else "0"} for i in range(6)])
    return warehouse.query("meter-A", t, t + 1800, TariffService(spec()))


if __name__ == "__main__":
    print(json.dumps(normal(), sort_keys=True))
