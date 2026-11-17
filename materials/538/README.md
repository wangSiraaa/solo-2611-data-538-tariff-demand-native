# Tariff catalogue and load facts

The current deployment handles the ordinary UTC/equal-five-minute example. TariffService supplies UTC bands to catalogue clients; LoadWarehouse stores load intervals and asks any provider for bands.

Run the existing example:

```sh
cd materials/538
python3 probe.py
```

`tariff.py` contains the independent tariff catalogue. `load.py` contains the load fact/query API. `probe.py` supplies the normal example. The requested behavior and legal input range are defined by the task prompt.
